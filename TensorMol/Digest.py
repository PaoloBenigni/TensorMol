from Mol import *
from Util import *
import numpy,os,sys,re
import cPickle as pickle
import LinearOperations
if (HAS_EMB):
	import MolEmb

class Digester:
	"""
	 An Embedding gives some chemical description of a molecular
	 Environment around a point. This one is for networks that will embed properties of atoms.
	 Molecule embeddings and Behler-Parrinello are in DigestMol.

	 A Digester samples a molecule using an embedding.
	 Because the embedding is evaluated so much, it's written in C.
	 please refer to /C_API/setup.py
	 The Default is Coulomb, but this is also the gen. interface
	 The embedding does not provide labels.
	"""
	def __init__(self, eles_, name_="GauSH", OType_="Disp"):
		"""
		Args:
			eles_ : a list of elements in the Tensordata that I'll digest
			name_: type of digester to reduce molecules to NN inputs.
			OType_: property of the molecule which will be learned (energy, force, etc)
		"""

		 # In Atomic units at 300K
		# These are the key variables which determine the type of digestion.
		self.name = name_ # Embedding type.
		self.eshape=None  #shape of an embedded case
		self.lshape=None  #shape of the labels of an embedded case.
		self.OType = OType_ # Output Type: HardP, SmoothP, StoP, Disp, Force, Energy etc. See Emb() for options.

		self.NTrainSamples=1 # Samples per atom. Should be made a parameter.
		if (self.OType == "SmoothP" or self.OType == "Disp"):
			self.NTrainSamples=1 #Smoothprobability only needs one sample because it fits the go-probability and pgaussians-center.

		self.eles = np.array(eles_)
		self.eles.sort() # Consistent list of atoms in the order they are treated.
		self.neles = len(eles_) # Consistent list of atoms in the order they are treated.
		self.nsym = self.neles+(self.neles+1)*self.neles  # channel of sym functions
		self.npgaussian = self.neles # channel of PGaussian
		# Instead self.emb should know it's return shape or it should be testable.

		self.SamplingType = PARAMS["dig_SamplingType"]
		self.TrainSampDistance=2.0 #how far in Angs to sample on average.
		self.ngrid = PARAMS["dig_ngrid"] #this is a shitty parameter if we go with anything other than RDF and should be replaced.
		self.BlurRadius = PARAMS["BlurRadius"] # Stdev of gaussian used as prob of atom
		self.SensRadius=6.0 # Distance which is used for input.

		# These are used to normalize data.
		self.MeanNorm=0.0
		self.StdNorm=1.0

		self.embtime=0.0
		self.outtime=0.0

		self.Print()
		return

	def AssignNormalization(self,mn,sn):
		self.MeanNorm=mn
		self.StdNorm=sn
		return

	def Print(self):
		LOGGER.info("-------------------- ")
		LOGGER.info("Digester Information ")
		LOGGER.info("self.name:"+self.name)
		LOGGER.info("self.OType"+self.OType)
		LOGGER.debug("self.NTrainSamples"+str(self.NTrainSamples))
		LOGGER.debug("self.TrainSampDistance"+str(self.TrainSampDistance))
		LOGGER.debug("self.OType"+self.OType)
		LOGGER.info("-------------------- ")
		return

	def MakeSamples_v2(self,point):    # with sampling function f(x)=M/(x+1)^2+N; f(0)=maxdisp,f(maxdisp)=0; when maxdisp =5.0, 38 % lie in (0, 0.1)
		disps = samplingfunc_v2(self.TrainSampDistance * np.random.random(self.NTrainSamples), self.TrainSampDistance)
		theta  = np.random.random(self.NTrainSamples)* math.pi
		phi = np.random.random(self.NTrainSamples)* math.pi * 2
		grids  = np.zeros((self.NTrainSamples,3),dtype=np.float32)
		grids[:,0] = disps*np.cos(theta)
		grids[:,1] = disps*np.sin(theta)*np.cos(phi)
		grids[:,2] = disps*np.sin(theta)*np.sin(phi)
		return grids + point

	def Blurs(self, diffs):
		dists=np.array(map(np.linalg.norm,diffs))
		return np.exp(dists*dists/(-1.0*self.BlurRadius*self.BlurRadius))/(np.power(2.0*Pi*self.BlurRadius*self.BlurRadius,3.0/2.0))

	def HardCut(self, diffs, cutoff=0.05):
		# 0, 1 output
		dists=np.array(map(np.linalg.norm,diffs))
		labels = np.clip(-(dists - cutoff), 0, (-(dists - cutoff)).max())
		labels[np.where(labels > 0)]=1
		return labels

#
#  Embedding functions, called by batch digests. Use outside of Digester() is discouraged.
#  Instead call a batch digest routine.
#

	def Emb(self, mol_, at_, xyz_, MakeOutputs=True, MakeGradients=False):
		"""
		Generates various molecular embeddings.
		Args:
			mol_: a Molecule to be digested
			at_: an atom to be digested or moved. if at_ < 0 it usually returns arrays for each atom in the molecule
			xyz_: makes inputs with at_ moved to these positions.
			MakeOutputs: generates outputs according to self.OType.
		Returns:
			Output embeddings, and possibly labels and gradients.
			if at_ < 0 the first dimension loops over atoms in mol_
		"""
		#start = time.time()
		if (self.name=="Coulomb"):
			Ins= MolEmb.Make_CM(mol_.coords, xyz_, mol_.atoms , self.eles ,  self.SensRadius, self.ngrid, at_, 0.0)
		elif (self.name=="GauSH"):
			Ins =  MolEmb.Make_SH(PARAMS, mol_.coords, mol_.atoms, at_);
		elif (self.name=="GauInv"):
			Ins= MolEmb.Make_Inv(PARAMS, mol_.coords, mol_.atoms, at_)
		elif (self.name=="RDF"):
			Ins= MolEmb.Make_RDF(mol_.coords, xyz_, mol_.atoms , self.eles ,  self.SensRadius, self.ngrid, at_, 0.0)
		elif (self.name=="SensoryBasis"):
			Ins= mol_.OverlapEmbeddings(mol_.coords, xyz_, mol_.atoms , self.eles ,  self.SensRadius, self.ngrid, at_, 0.0)
		elif (self.name=="SymFunc"):
			Ins= self.make_sym(mol_.coords, xyz_, mol_.atoms , self.eles ,  self.SensRadius, self.ngrid, at_, 0.0)
		elif (self.name=="PGaussian"):
			Ins= self.make_pgaussian(mol_.coords, xyz_, mol_.atoms , self.eles ,  self.SensRadius, self.ngrid, at_, 0.0)
		else:
			raise Exception("Unknown Embedding Function.")
		#self.embtime += (time.time() - start)
		#start = time.time()
		Outs=None
		if (MakeOutputs):
			if (self.OType=="HardP"):
				Outs = self.HardCut(xyz_-coords_[at_])
			elif (self.OType=="SmoothP"):
				Outs = mol_.FitGoProb(at_)
			elif (self.OType=="Disp"):
				Outs = mol_.GoDisp(at_)
			elif (self.OType=="GoForce"):
				Outs = mol_.GoForce(at_)
			elif (self.OType=="GoForceSphere"):
				Outs = mol_.GoForce(at_, 1) # See if the network is better at doing spherical=>spherical
			elif (self.OType=="Force"):
				if ( "forces" in mol_.properties):
					if (at_<0):
						Outs = mol_.properties['forces']
						#print "Outs", Outs
					else:
						Outs = mol_.properties['forces'][at_].reshape((1,3))
				else:
					raise Exception("Mol Is missing force. ")
			elif (self.OType=="StoP"):
				ens_ = mol_.EnergiesOfAtomMoves(xyz_,at_)
				if (ens_==None):
					raise Exception("Empty energies...")
				E0=np.min(ens_)
				Es=ens_-E0
				Boltz=np.exp(-1.0*Es/KAYBEETEE)
				rnds = np.random.rand(len(xyz_))
				Outs = np.array([1 if rnds[i]<Boltz[i] else 0 for i in range(len(ens_))])
			elif (self.OType=="Energy"):
				if ("energy" in mol_.properties):
					ens_ = mol_.properties["energy"]
				else:
					raise Exception("Empty energies...")
			elif (self.OType=="AtomizationEnergy"):
				if ("atomization" in mol_.properties):
					ens_ = mol_.properties["atomization"]
				else:
					raise Exception("Empty energies...")
			elif (self.OType=="CalcEnergy"):
				ens_ = mol_.EnergiesOfAtomMoves(xyz_,at_)
				if (ens_==None):
					raise Exception("Empty energies...")
				E0=np.min(ens_)
				Es=ens_-E0
				Outs = Es
			# elif (self.OType=="Force"):
			# 	ens_ = mol_.ForcesAfterAtomMove(xyz_,at_)
			# 	if (ens_==None):
			# 		raise Exception("Empty energies...")
			elif (self.OType=="GoForce_old_version"): # python version is fine for here
				ens_ = mol_.SoftCutGoForceOneAtom(at_)
				Outs = ens_
				if (ens_==None):
					raise Exception("Empty energies...")
			else:
				raise Exception("Unknown Digester Output Type.")
			#self.outtime += (time.time() - start)
			#print "Embtime: ", self.embtime, " OutTime: ", self.outtime
			return Ins,Outs
		else:
			return Ins

	def unscld(self,a):
		"""
		I really don't like this routine at all. I think we should give
		Digesters some sort of Translator object which allows the networks training
		target to be some simple transformations of the input/output
		without changing the digester, redoing training etc...
		Thinking about how to do this for all elements etc. is tricky.
		"""
		return (a*self.StdNorm+self.MeanNorm)

	def unlog(self, a):
		tmp = a.copy()
		for x in np.nditer(tmp, op_flags=["readwrite"]):
			if x > 0:
				x[...] = (10**x)-1
			if x < 0:
				x[...] = (-1*(10**(-x)))+1
		return tmp

	def EvaluateTestOutputs(self, desired, predicted):
		try:
			print "Evaluating, ", len(desired), " predictions... "
			#print desired.shape, predicted.shape
			if (self.OType=="HardP"):
				raise Exception("Unknown Digester Output Type.")
			elif (self.OType=="Disp" or self.OType=="Force" or self.OType == "GoForce"):
				LOGGER.info("Test displacement errors direct (mean,std) %f,%f",np.average(ders),np.std(ders))
				LOGGER.info("Average learning target: %s, Average output (direct) %s", str(np.average(desired[:,-3:],axis=0)),str(np.average(predicted[:,-3:],axis=0)))
				print "Fraction of incorrect directions: ", np.sum(np.sign(desired[:,-3:])-np.sign(predicted[:,-3:]))/(6.*len(desired))
				ders=np.zeros(len(desired))
				#comp=np.zeros(len(desired))
				if (PARAMS["NormalizeOutputs"]):
					for i in range(len(desired)):
						ders[i] = np.linalg.norm(self.unscld(predicted[i,-3:])-self.unscld(desired[i,-3:]))
					for i in range(100):
						print "Desired: ",i,self.unscld(desired[i,-3:])," Predicted: ",self.unscld(predicted[i,-3:])
				elif (PARAMS["NormalizeOutputsLog"]):
					for i in range(len(desired)):
						ders[i] = np.linalg.norm(self.unlog(predicted[i,-3:])-self.unlog(desired[i,-3:]))
					for i in range(100):
						print "Desired: ",i,self.unscld(desired[i,-3:])," Predicted: ",self.unscld(predicted[i,-3:])
				LOGGER.info("Test displacement errors direct (mean,std) %f,%f",np.average(ders),np.std(ders))
				LOGGER.info("Average learning target: %s, Average output (direct) %s", str(np.average(desired[:,-3:],axis=0)),str(np.average(predicted[:,-3:],axis=0)))
				print "Fraction of incorrect directions: ", np.sum(np.sign(desired[:,-3:])-np.sign(predicted[:,-3:]))/(6.*len(desired))
			elif (self.OType == "GoForceSphere"):
				# Convert them back to cartesian
				desiredc = SphereToCartV(desired)
				predictedc = SphereToCartV(predicted)
				ders=np.zeros(len(desired))
				#comp=np.zeros(len(desired))
				for i in range(len(desiredc)):
					ders[i] = np.linalg.norm(self.unscld(predictedc[i,-3:])-self.unscld(desiredc[i,-3:]))
				LOGGER.info("Test displacement errors direct (mean,std) %f,%f",np.average(ders),np.std(ders))
				LOGGER.info("Average learning target: %s, Average output (direct) %s", str(np.average(desiredc[:,-3:],axis=0)),str(np.average(predictedc[:,-3:],axis=0)))
				print "Fraction of incorrect directions: ", np.sum(np.sign(desiredc[:,-3:])-np.sign(predictedc[:,-3:]))/(6.*len(desiredc))
				for i in range(100):
					print "Desired: ",i,self.unscld(desiredc[i,-3:])," Predicted: ",self.unscld(predictedc[i,-3:])
			elif (self.OType=="SmoothP"):
				ders=np.zeros(len(desired))
				iers=np.zeros(len(desired))
				comp=np.zeros(len(desired))
				for i in range(len(desired)):
					#print "Direct - desired disp", desired[i,-3:]," Pred disp", predicted[i,-3:]
					Pr = GRIDS.Rasterize(predicted[i,:GRIDS.NGau3])
					Pr /= np.sum(Pr)
					p=np.dot(GRIDS.MyGrid().T,Pr)
					#print "fit disp: ", p
					ders[i] = np.linalg.norm(predicted[i,-3:]-desired[i,-3:])
					iers[i] = np.linalg.norm(p-desired[i,-3:])
					comp[i] = np.linalg.norm(p-predicted[i,-3:])
				print "Test displacement errors direct (mean,std) ", np.average(ders),np.std(ders), " indirect ",np.average(iers),np.std(iers), " Comp ", np.average(comp), np.std(comp)
				print "Average learning target: ", np.average(desired[:,-3:],axis=0),"Average output (direct)",np.average(predicted[:,-3:],axis=0)
				print "Fraction of incorrect directions: ", np.sum(np.sign(desired[:,-3:])-np.sign(predicted[:,-3:]))/(6.*len(desired))
			elif (self.OType=="StoP"):
				raise Exception("Unknown Digester Output Type.")
			elif (self.OType=="Energy"):
				raise Exception("Unknown Digester Output Type.")
			elif (self.OType=="GoForce_old_version"): # python version is fine for here
				raise Exception("Unknown Digester Output Type.")
			else:
				raise Exception("Unknown Digester Output Type.")
		except Exception as Ex:
			print "Something went wrong"
			pass
		return

#
#  Various types of Batch Digests.
#

	def TrainDigestMolwise(self, mol_):
		"""
		Returns list of inputs and outputs for a molecule.
		Uses self.Emb() uses Mol to get the Desired output type (Energy,Force,Probability etc.)
		This version works mol-wise to try to speed up and avoid calling C++ so much...
		Args:
			mol_: a molecule to be digested
			eles_: A list of elements coming from Tensordata to order the output.
		Returns:
			Two lists: containing inputs and outputs in order of eles_
		"""
		if (((self.name != "GauInv" and self.name !="GauSH")) or (self.OType != "GoForce" and self.OType!="GoForceSphere" and self.OType!="Force" )):
			raise Exception("Molwise Embedding not supported")
		if (self.eshape==None or self.lshape==None):
			tinps, touts = self.Emb(mol_,0,np.array([[0.0,0.0,0.0]]))
			self.eshape = list(tinps[0].shape)
			self.lshape = list(touts[0].shape)
			LOGGER.debug("Assigned Digester shapes: "+str(self.eshape)+str(self.lshape))
		return self.Emb(mol_,-1,mol_.coords[0]) # will deal with getting energies if it's needed.

	def TrainDigest(self, mol_, ele_, MakeDebug=False):
		"""
		Returns list of inputs and outputs for a molecule.
		Uses self.Emb() uses Mol to get the Desired output type (Energy,Force,Probability etc.)
		Args:
			mol_: a molecule to be digested
			ele_: an element for which training data will be made.
			MakeDebug: if MakeDebug is True, it also returns a list with debug information to trace possible errors in digestion.
		"""
		if (self.eshape==None or self.lshape==None):
			tinps, touts = self.Emb(mol_,0,np.array([[0.0,0.0,0.0]]))
			self.eshape = list(tinps[0].shape)
			self.lshape = list(touts[0].shape)
			LOGGER.debug("Assigned Digester shapes: "+str(self.eshape)+str(self.lshape))
		ncase = mol_.NumOfAtomsE(ele_)*self.NTrainSamples
		ins = np.zeros(shape=tuple([ncase]+list(self.eshape)),dtype=np.float32)
		outs = np.zeros(shape=tuple([ncase]+list(self.lshape)),dtype=np.float32)
		dbg=[]
		casep=0
		for i in range(len(mol_.atoms)):
			if (mol_.atoms[i]==ele_):
				if (self.OType == "SmoothP" or self.OType == "Disp" or self.OType == "Force"):
					inputs, outputs = self.Emb(mol_,i,mol_.coords[i]) # will deal with getting energies if it's needed.
				elif(self.SamplingType=="Smooth"): #If Smooth is now a property of the Digester: OType SmoothP
					samps=PointsNear(mol_.coords[i], self.NTrainSamples, self.TrainSampDistance)
					inputs, outputs = self.Emb(mol_,i,samps) # will deal with getting energies if it's needed.
				else:
					samps=self.MakeSamples_v2(mol_.coords[i])
					inputs, outputs = self.Emb(mol_,i,samps)
				# Here we should write a short routine to debug/print the inputs and outputs.
				#				print "Smooth",outputs
				#print i, mol_.atoms, mol_.coords,mol_.coords[i],"Samples:",samps,"inputs ", inputs, "Outputs",outputs, "Distances",np.array(map(np.linalg.norm,samps-mol_.coords[i]))

				ins[casep:casep+self.NTrainSamples] = np.array(inputs)
				outs[casep:casep+self.NTrainSamples] = outputs
				casep += self.NTrainSamples

		if (MakeDebug):
			return ins,outs,dbg
		else:
			return ins,outs

	def SampleDigestWPyscf(self, mol_, ele_,uniform=False):
		''' Runs PySCF calculations for each sample without generating embeddings and probabilities '''
		for i in range(len(mol_.atoms)):
			if (mol_.atoms[i]==ele_):
				samps=None
				if (not uniform):
					samps=self.MakeSamples(mol_.coords[i])
				else:
					samps=MakeUniform(mol_.coords[i],4.0,20)
				energies=mol_.RunPySCFWithCoords(samps,i)

	def UniformDigest(self, mol_, at_, mxstep, num):
		""" Returns list of inputs sampled on a uniform cubic grid around at """
		ncase = num*num*num
		samps=MakeUniform(mol_.coords[at_],mxstep,num)
		if (self.name=="SymFunc"):
			inputs = self.Emb(self, mol_, at_, samps, None, False) #(self.EmbF())(mol_.coords, samps, mol_.atoms, self.eles ,  self.SensRadius, self.ngrid, at_, 0.0)
			inputs = np.asarray(inputs)
		else:
			inputs = self.Emb(self, mol_, at_, samps, None, False)
			inputs = np.assrray(inputs[0])
		return samps, inputs

	def emb_vary_coords(self, coords, xyz, atoms, eles, Radius, ngrid, vary_at, tar_at):
		return  MolEmb.Make_CM_vary_coords(coords, xyz, atoms, eles, Radius, ngrid, vary_at, tar_at)

	def make_sym(self, coords_, xyz_, ats_,  eles , SensRadius, ngrid, at_, dummy):    #coords_, xyz_, ats_, self.eles ,  self.SensRadius, self.ngrid, at_, 0.0
		zeta=[]
		eta1=[]
		eta2=[]
		Rs=[]
		for i in range (0, ngrid):
			zeta.append(1.5**i)    # set some value for zeta, eta, Rs
			eta1.append(0.008*(2**i))
			eta2.append(0.002*(2**i))
			Rs.append(i*SensRadius/float(ngrid))
		SYM =  MolEmb.Make_Sym(coords_, xyz_, ats_, eles, at_, SensRadius, zeta, eta1, eta2, Rs)
		SYM = numpy.asarray(SYM[0], dtype=np.float32)
		SYM = SYM.reshape((SYM.shape[0]/self.nsym, self.nsym,  SYM.shape[1] *  SYM.shape[2]))
		return SYM

	def make_pgaussian (self, coords_, xyz_, ats_, eles_, SensRadius, ngrid, at_, dummy):
		eta = []
		eta_max = 12  # hard code
		eta_min = 0.5 #	hard code
		for i in range (0, ngrid):
			tmp=math.log(eta_max/eta_min)/(ngrid-1)*i
			eta.append(pow(math.e, tmp)*eta_min)
		PGaussian = MolEmb.Make_PGaussian(coords_, xyz_, ats_, eles_, at_, SensRadius, eta)
		PGaussian = numpy.asarray(PGaussian[0], dtype=np.float32)
                PGaussian = PGaussian.reshape((PGaussian.shape[0]/self.npgaussian, self.npgaussian,  PGaussian.shape[1] *  PGaussian.shape[2]))
                return PGaussian
